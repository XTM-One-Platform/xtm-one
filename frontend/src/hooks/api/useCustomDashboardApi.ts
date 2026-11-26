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

import { useCallback, useEffect, useRef } from "react";
import { queryOptions, useMutation, useQueries, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { API_BASE, api, notFoundIsOrdinary } from "@/lib/api";

import { markDashboardFresh, readDashboardData, useDashboardAccess } from "./dashboard-access";
import type {
  CustomDashboard,
  CustomDashboardAsset,
  CustomDashboardOperation,
  DashboardPublicLink,
  DashboardVersionDiff,
  PlatformFormsResponse,
  PublicDashboard,
  CustomDashboardSharingUpdate,
  CustomDashboardSummary,
  CustomDashboardUpdate,
  CustomDashboardVersion,
  WidgetData,
  WidgetDataRange,
} from "@/types";

const DASHBOARDS_KEY = ["custom-dashboards"] as const;

/**
 * Refresh the cached dashboards (all, or one with its versions and
 * publications) - never their live widget data: it is keyed on what each
 * widget reads (`useWidgetData`), so only a widget a change touched reads its
 * platform again.
 */
export function invalidateDashboards(qc: QueryClient, id?: string) {
  return qc.invalidateQueries({
    queryKey: id ? [...DASHBOARDS_KEY, id] : DASHBOARDS_KEY,
    predicate: (query) => query.queryKey[2] !== "widget-data",
  });
}

/** The dashboards the caller may open - their own, shared with them, company
 *  ones - most recently updated first. */
export function useCustomDashboards(enabled = true) {
  return useQuery<CustomDashboardSummary[]>({
    queryKey: [...DASHBOARDS_KEY, "list"],
    queryFn: () => api.get("/custom-dashboards"),
    enabled,
  });
}

/** Read every widget and data source of a dashboard again, from its platforms (the page's Refresh). */
export function useRefreshDashboard(dashboardId: string) {
  const qc = useQueryClient();
  return useCallback(() => {
    markDashboardFresh(dashboardId);
    return qc.invalidateQueries({
      queryKey: [...DASHBOARDS_KEY, dashboardId],
      predicate: (query) => query.queryKey[2] === "widget-data" || query.queryKey[2] === "source-data",
    });
  }, [qc, dashboardId]);
}

/** One published link, as administrators review them all. */
export interface DashboardPublicLinkListItem {
  dashboard_id: string;
  dashboard_title: string;
  publisher_name: string | null;
  created_at: string | null;
  refresh_minutes: number;
  snapshot_at: string | null;
}

/** Every published dashboard link (administrators). */
export function useDashboardPublicLinks(enabled: boolean) {
  return useQuery<DashboardPublicLinkListItem[]>({
    queryKey: [...DASHBOARDS_KEY, "public-links"],
    queryFn: () => api.get("/custom-dashboards/public-links"),
    enabled,
  });
}

/** Turn any published link off (administrators). */
export function useTurnOffDashboardPublicLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (dashboardId: string) => api.delete(`/custom-dashboards/public-links/${dashboardId}`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}

/** File a dashboard under tags (the whole list); its definition is untouched. */
export function useSetDashboardTags() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, tags }: { id: string; tags: string[] }) => api.put<CustomDashboard>(`/custom-dashboards/${id}/tags`, { tags }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}

/** A private copy of a dashboard the caller may read. */
export function useDuplicateCustomDashboard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<CustomDashboard>(`/custom-dashboards/${id}/duplicate`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}

/** An empty dashboard of the caller's, to fill in the studio. */
export function useCreateCustomDashboard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; description?: string }) => api.post<CustomDashboard>("/custom-dashboards", body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}

/** A dashboard of the caller's from a file (an XTM One pack, an OpenCTI or OpenAEV export), with what the
 *  conversion changed or left out. */
export function useImportCustomDashboard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ file, bindings }: { file: File; bindings?: Record<string, string> }) => {
      const form = new FormData();
      form.append("file", file);
      if (bindings && Object.keys(bindings).length) form.append("bindings", JSON.stringify(bindings));
      return api.postForm<CustomDashboard & { import_notes: string[] }>("/custom-dashboards/import", form);
    },
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}

/** One dashboard with its spec. An agent may refresh it at any time (a
 *  scheduled assignment), so it is read again when the window regains focus. */
export function useCustomDashboard(id: string | undefined) {
  return useQuery<CustomDashboard>({
    queryKey: [...DASHBOARDS_KEY, id],
    queryFn: () => api.get(`/custom-dashboards/${id}`),
    enabled: !!id,
    refetchOnWindowFocus: true,
    // A deleted dashboard is an ordinary answer: the viewer says so, and the
    // dashboard chosen for /dashboard gives way to the platform's.
    ...notFoundIsOrdinary,
  });
}

/**
 * Rename a dashboard, remove or move widgets, at the revision the page read.
 * Widgets are only ever written by the agent tools, which validate them; this
 * edit is re-validated server-side the same way. A 409 means an agent
 * refreshed the dashboard meanwhile: the caller shows the new revision.
 */
/** `quiet`: the caller says why a save was refused, so the global error toast stays out. */
function useUpdateCustomDashboard(id: string, { quiet = false }: { quiet?: boolean } = {}) {
  const qc = useQueryClient();
  return useMutation({
    meta: { suppressGlobalErrorToast: quiet },
    mutationFn: (body: CustomDashboardUpdate) => api.patch<CustomDashboard>(`/custom-dashboards/${id}`, body),
    onSuccess: (dashboard) => {
      // `can_use_as_home` is computed on GET only: keep the one read.
      qc.setQueryData([...DASHBOARDS_KEY, id], (previous: CustomDashboard | undefined) =>
        previous ? { ...dashboard, can_use_as_home: previous.can_use_as_home } : dashboard,
      );
      void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, "list"] });
    },
    onError: () => {
      void invalidateDashboards(qc, id);
    },
  });
}

interface QueuedEditHandlers {
  onSuccess?: (saved: CustomDashboard) => void;
  onError?: (error: Error & { status?: number }) => void;
}

/**
 * Edits saved one after the other, each at the revision the previous one
 * wrote: a drag that ends while the last one is still being saved does not
 * meet a conflict of its own making. A change made elsewhere meanwhile is
 * still a conflict (`onError` with a 409). `replace` writes a whole definition
 * in the same queue (an edit too large for one `PATCH`: the canvas's undo of
 * many elements at once). An edit whose caller says why it was refused
 * (`onError`) shows no global error toast as well.
 */
export function useQueuedDashboardEdit(id: string, revision: number | undefined) {
  const update = useUpdateCustomDashboard(id);
  // An edit whose caller handles its refusal: one message, the caller's.
  const quietUpdate = useUpdateCustomDashboard(id, { quiet: true });
  const replaceDefinition = useReplaceDashboardDefinition(id);
  const latest = useRef(revision ?? 0);
  const chain = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => {
    if (revision !== undefined && revision > latest.current) latest.current = revision;
  }, [revision]);
  const enqueue = useCallback((save: (revision: number) => Promise<CustomDashboard>, handlers: QueuedEditHandlers) => {
    chain.current = chain.current.then(async () => {
      try {
        const saved = await save(latest.current);
        latest.current = saved.revision;
        handlers.onSuccess?.(saved);
      } catch (error) {
        handlers.onError?.(error as Error & { status?: number });
      }
    });
  }, []);
  const edit = useCallback(
    (operations: CustomDashboardOperation[], handlers: QueuedEditHandlers = {}) =>
      enqueue((at) => (handlers.onError ? quietUpdate : update).mutateAsync({ revision: at, operations }), handlers),
    [enqueue, quietUpdate, update],
  );
  const replace = useCallback(
    (definition: unknown, handlers: QueuedEditHandlers = {}) =>
      enqueue((at) => replaceDefinition.mutateAsync({ revision: at, definition }), handlers),
    [enqueue, replaceDefinition],
  );
  return { edit, replace, isPending: update.isPending || quietUpdate.isPending || replaceDefinition.isPending };
}

export function useDeleteCustomDashboard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/custom-dashboards/${id}`),
    onSuccess: (_data, id) => {
      qc.removeQueries({ queryKey: [...DASHBOARDS_KEY, id] });
      void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, "list"] });
    },
  });
}

/**
 * The live data of one platform widget of a stored dashboard, read by the
 * server with the viewer's own access to its integration. The widget is read
 * from the stored dashboard: the request only names it and picks a range. An
 * error is an ordinary answer (`status: "error"` with a fixed code), so it is
 * not retried; the server caches answers for a minute.
 */
export function useWidgetData(
  dashboardId: string | undefined,
  widgetId: string,
  range: WidgetDataRange | undefined,
  /** What the stored widget reads (`fingerprint` of the spec reader): a change reads again. */
  fingerprint: string,
  enabled = true,
  /** Its source's `refresh`, in seconds: read again that often (and a question being answered every few seconds). */
  refresh?: number,
  /** The page's tokens its source uses (`tokens.tokensFor`): sent with the read, a change reads again. */
  tokens: Record<string, string | string[]> = {},
) {
  const access = useDashboardAccess();
  return useQuery<WidgetData>({
    queryKey: widgetDataKey(dashboardId, widgetId, range, fingerprint, tokens),
    queryFn: () =>
      readDashboardData(access, dashboardId ?? "", "widgets", widgetId, {
        ...(range ? { range } : {}),
        ...(Object.keys(tokens).length ? { tokens } : {}),
      }),
    enabled: enabled && !!dashboardId,
    staleTime: 60_000,
    retry: false,
    refetchInterval: (query) => {
      // A snapshot is what it is; an export reads once.
      if (access.kind !== "session") return false;
      const data = query.state.data;
      if (data?.status === "error" && data.code === "running") return ASK_POLL_MS;
      return refresh ? refresh * 1000 : false;
    },
  });
}

/** The cache key of a widget's data: the export and Inspect read what the widget drew. */
export function widgetDataKey(
  dashboardId: string | undefined,
  widgetId: string,
  range: WidgetDataRange | undefined,
  fingerprint: string,
  tokens: Record<string, string | string[]> = {},
) {
  return [...DASHBOARDS_KEY, dashboardId, "widget-data", widgetId, range ?? "default", fingerprint, tokens] as const;
}

/**
 * One data source's data, read with the page's tokens: what an input lists
 * and what a result token (`<source>:result.<field>`) holds.
 */
export function useSourceData(
  dashboardId: string | undefined,
  sourceId: string | undefined,
  tokens: Record<string, string | string[]>,
  fingerprint: string,
) {
  const access = useDashboardAccess();
  return useQuery<WidgetData>({
    queryKey: [...DASHBOARDS_KEY, dashboardId, "source-data", sourceId, fingerprint, tokens],
    queryFn: () =>
      readDashboardData(access, dashboardId ?? "", "data-sources", sourceId ?? "", {
        ...(Object.keys(tokens).length ? { tokens } : {}),
      }),
    enabled: !!dashboardId && !!sourceId,
    staleTime: 60_000,
    retry: false,
  });
}

/** A platform source editor's question (`POST /custom-dashboards/platform-schema`). */
export interface PlatformSchemaQuestion {
  source: string;
  integration_id: string;
  /** `entities`: entities of `types` matching `search`, or named by their `ids` (an entity input's choices). */
  kind: "entity_types" | "filter_keys" | "attributes" | "values" | "labels" | "entities" | "saved_filters";
  perspective?: string;
  types?: string[];
  key?: string;
  search?: string;
  ids?: string[];
}

/** One option of an answer; a filter key or a property carries its definition too. */
interface PlatformSchemaItem {
  value?: string;
  label: string;
  group?: string;
  key?: string;
  type?: string;
  multiple?: boolean;
  search?: string[];
  values?: string[];
  entity?: string;
  window?: boolean;
}

/**
 * What a platform source editor needs (types, filter keys, attributes,
 * values), read by the server through the person's own
 * integration. A refusal is an ordinary answer with a fixed code.
 */
export function usePlatformSchema(question: PlatformSchemaQuestion | null) {
  return useQuery(platformSchemaQuery(question));
}

/** An answer of `POST /custom-dashboards/platform-schema`. */
interface PlatformSchemaAnswer {
  status: "ok" | "error";
  items: PlatformSchemaItem[];
  code: string | null;
}

function platformSchemaQuery(question: PlatformSchemaQuestion | null) {
  return queryOptions<PlatformSchemaAnswer>({
    queryKey: [...DASHBOARDS_KEY, "platform-schema", question],
    queryFn: () => api.post("/custom-dashboards/platform-schema", question),
    enabled: !!question?.integration_id,
    staleTime: 60_000,
    retry: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Several schema questions at once (a form scope's sources); `null` is a question not asked. */
export function usePlatformSchemas(questions: (PlatformSchemaQuestion | null)[]) {
  return useQueries({ queries: questions.map(platformSchemaQuery) });
}

/** Each platform's widget editor, as the description the one source form draws (the same for everyone). */
export const platformFormsQuery = queryOptions<PlatformFormsResponse>({
  queryKey: [...DASHBOARDS_KEY, "platform-forms"],
  queryFn: () => api.get("/custom-dashboards/platform-forms"),
  staleTime: Infinity,
  retry: false,
  meta: { suppressGlobalErrorToast: true },
});

export function usePlatformForms() {
  return useQuery(platformFormsQuery);
}

/** A platform widget not saved yet, read with the person's access (`POST .../widgets/preview`, editors). */
export function usePreviewPlatformWidget(dashboardId: string) {
  return useMutation({
    mutationFn: (widget: Record<string, unknown>) =>
      api.post<WidgetData>(`/custom-dashboards/${dashboardId}/widgets/preview`, { widget }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** How often a question being answered is read again. */
const ASK_POLL_MS = 3_000;

/**
 * Ask an `ask` data source's question as oneself (`POST .../data-sources/{id}/ask`):
 * the run is the person's, its answer read by the widgets drawing the source.
 * A refusal is an ordinary answer (`status: "error"` with a fixed code).
 */
export function useAskDataSource(dashboardId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      api.post<WidgetData>(`/custom-dashboards/${dashboardId}/data-sources/${encodeURIComponent(sourceId)}/ask`, {}),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, dashboardId, "widget-data"] }),
  });
}

/**
 * Refresh now: a source refreshed on the server read again by the platform, as
 * its publisher (`POST .../data-sources/<id>/refresh`, at most every 30 s); the
 * widgets drawing it read the new snapshot.
 */
export function useRefreshSnapshot(dashboardId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      api.post<WidgetData>(`/custom-dashboards/${dashboardId}/data-sources/${encodeURIComponent(sourceId)}/refresh`, {}),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, dashboardId, "widget-data"] }),
  });
}

/**
 * The open pages of a dashboard, told a source has a new snapshot
 * (`dashboard.source_updated` on the activity stream): its widgets, and that
 * source's own reads, are read again.
 */
export function invalidateDashboardSource(qc: ReturnType<typeof useQueryClient>, dashboardId: string, sourceId: string | undefined) {
  return qc.invalidateQueries({
    queryKey: [...DASHBOARDS_KEY, dashboardId],
    predicate: (query) =>
      query.queryKey[2] === "widget-data" || (query.queryKey[2] === "source-data" && (!sourceId || query.queryKey[3] === sourceId)),
  });
}

/**
 * The data of one data source of a stored dashboard (a visualization's
 * annotations), read like `useWidgetData`: by its id, from the stored
 * definition, with the viewer's own access.
 */
export function useDataSourceData(
  dashboardId: string | undefined,
  sourceId: string | undefined,
  range: WidgetDataRange | undefined,
  /** What the stored source reads: a change reads again. */
  fingerprint: string,
) {
  const access = useDashboardAccess();
  return useQuery<WidgetData>({
    queryKey: [...DASHBOARDS_KEY, dashboardId, "widget-data", "source", sourceId, range ?? "default", fingerprint],
    queryFn: () => readDashboardData(access, dashboardId ?? "", "data-sources", sourceId ?? "", range ? { range } : {}),
    enabled: !!dashboardId && !!sourceId,
    staleTime: 60_000,
    retry: false,
  });
}

/** Change who may open or edit a dashboard. The spec and the revision are untouched. */
export function useUpdateDashboardSharing(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CustomDashboardSharingUpdate) => api.put<CustomDashboard>(`/custom-dashboards/${id}/sharing`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (dashboard) => {
      qc.setQueryData([...DASHBOARDS_KEY, id], (previous: CustomDashboard | undefined) =>
        previous ? { ...dashboard, can_use_as_home: previous.can_use_as_home } : dashboard,
      );
      void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, "list"] });
    },
  });
}

/** The kept versions of a dashboard, newest first, while `enabled`. */
export function useDashboardVersions(id: string, enabled: boolean) {
  return useQuery<CustomDashboardVersion[]>({
    queryKey: [...DASHBOARDS_KEY, id, "versions"],
    queryFn: () => api.get(`/custom-dashboards/${id}/versions`),
    enabled: enabled && !!id,
  });
}

/** One kept version with its spec, to preview it. */
export function useDashboardVersion(id: string, revision: number | null) {
  return useQuery<CustomDashboardVersion>({
    queryKey: [...DASHBOARDS_KEY, id, "versions", revision],
    queryFn: () => api.get(`/custom-dashboards/${id}/versions/${revision}`),
    enabled: !!id && revision !== null,
  });
}

/** Two kept versions compared (`GET .../versions/diff`), fetched once both are picked. */
export function useDashboardVersionDiff(id: string, from: number | null, to: number | null) {
  return useQuery<DashboardVersionDiff>({
    queryKey: [...DASHBOARDS_KEY, id, "versions", "diff", from, to],
    queryFn: () => api.get(`/custom-dashboards/${id}/versions/diff?from=${from}&to=${to}`),
    enabled: !!id && from !== null && to !== null && from !== to,
    staleTime: Infinity,
  });
}

/**
 * Write a dashboard's whole definition (the source editor), at the revision
 * the page read: validated server-side like an agent's write, a 422 saying
 * which field is wrong, a 409 when it changed meanwhile. The caller says so.
 */
export function useReplaceDashboardDefinition(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { revision: number; definition: unknown }) =>
      api.put<CustomDashboard>(`/custom-dashboards/${id}/definition`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (dashboard) => {
      qc.setQueryData([...DASHBOARDS_KEY, id], (previous: CustomDashboard | undefined) =>
        previous ? { ...dashboard, can_use_as_home: previous.can_use_as_home } : dashboard,
      );
      void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, "list"] });
    },
  });
}

/**
 * Upload an image or an SVG a canvas of the dashboard draws (edit access,
 * `DASHBOARD_STUDIO`): PNG, JPEG, GIF, WebP or SVG (cleaned server-side), 5 MB.
 * The caller says a refusal (a 422 naming the rule).
 */
export function useUploadDashboardAsset(id: string) {
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm<CustomDashboardAsset>(`/custom-dashboards/${id}/assets`, form);
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What a dashboard export renders (`POST .../render-export`). */
export interface DashboardRenderExport {
  format: "pdf" | "png";
  /** Its tabs, all by default. */
  layout_ids?: string[];
  /** One visualization alone, as a PNG. */
  visualization_id?: string;
}

/** A dashboard, or one visualization, rendered server-side as a PDF or a PNG with the caller's own access. */
export function useExportDashboard(id: string) {
  return useMutation({
    mutationFn: (body: DashboardRenderExport) => api.postDownload(`/custom-dashboards/${id}/render-export`, body),
    meta: { suppressGlobalErrorToast: true },
  });
}

// ── The published link ───────────────────────────────────────────────────────

/** The dashboard's published link (managers): whether the platform allows them, and its address and snapshot. */
export function useDashboardPublicLink(id: string, enabled: boolean) {
  return useQuery<DashboardPublicLink>({
    queryKey: [...DASHBOARDS_KEY, id, "public-link"],
    queryFn: () => api.get(`/custom-dashboards/${id}/public-link`),
    enabled: enabled && !!id,
    throwOnError: false,
  });
}

/** Publish, regenerate, change how often the snapshot is read, read it again now, or turn the link off. */
export function useDashboardPublicLinkActions(id: string) {
  const qc = useQueryClient();
  const key = [...DASHBOARDS_KEY, id, "public-link"];
  const store = (link: DashboardPublicLink) => qc.setQueryData(key, link);
  const options = { meta: { suppressGlobalErrorToast: true } };
  return {
    publish: useMutation({
      mutationFn: (body: { regenerate?: boolean; refresh_minutes?: number }) => api.post<DashboardPublicLink>(`/custom-dashboards/${id}/public-link`, body),
      onSuccess: store,
      ...options,
    }),
    setRefresh: useMutation({
      mutationFn: (refresh_minutes: number) => api.patch<DashboardPublicLink>(`/custom-dashboards/${id}/public-link`, { refresh_minutes }),
      onSuccess: store,
      ...options,
    }),
    setCeiling: useMutation({
      mutationFn: (marking_ceiling: string[]) => api.patch<DashboardPublicLink>(`/custom-dashboards/${id}/public-link`, { marking_ceiling }),
      onSuccess: store,
      ...options,
    }),
    readAgain: useMutation({
      mutationFn: () => api.post<DashboardPublicLink>(`/custom-dashboards/${id}/public-link/refresh`, {}),
      onSuccess: store,
      ...options,
    }),
    unpublish: useMutation({
      mutationFn: () => api.delete(`/custom-dashboards/${id}/public-link`),
      onSuccess: () => qc.invalidateQueries({ queryKey: key }),
      ...options,
    }),
  };
}

/** A failed read of a published link: the HTTP status (404 for any link that does not answer). */
export class PublicDashboardError extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

/** What a published link shows, read with no session (`GET /public/dashboards/{token}`). */
export function usePublicDashboard(token: string | undefined) {
  return useQuery<PublicDashboard, PublicDashboardError>({
    queryKey: ["public-dashboard", token],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/public/dashboards/${encodeURIComponent(token ?? "")}`, { credentials: "omit" });
      if (!res.ok) throw new PublicDashboardError(res.status);
      return (await res.json()) as PublicDashboard;
    },
    enabled: !!token,
    throwOnError: false,
    retry: (failures, error) => error.status !== 404 && failures < 2,
    refetchOnWindowFocus: false,
  });
}

/** Bring a version back as a new revision, at the revision the page read (409 when it moved on). */
export function useRestoreDashboardVersion(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ revision, current }: { revision: number; current: number }) =>
      api.post<CustomDashboard>(`/custom-dashboards/${id}/versions/${revision}/restore`, { revision: current }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => {
      void invalidateDashboards(qc, id);
      void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, "list"] });
    },
  });
}

/** Where a dashboard was published. */
export interface DashboardPublication {
  id: string;
  target: "opencti" | "opencti-custom-view" | "openaev" | "xtm_hub";
  revision: number;
  integration_id: string;
  remote_id: string;
  remote_url: string | null;
  published_by: string | null;
  published_at: string | null;
}

export function useDashboardPublications(id: string, enabled: boolean) {
  return useQuery<DashboardPublication[]>({
    queryKey: [...DASHBOARDS_KEY, id, "publications"],
    queryFn: () => api.get(`/custom-dashboards/${id}/publications`),
    enabled: enabled && !!id,
  });
}

/** Publish a dashboard to OpenCTI or OpenAEV as one of theirs, with the caller's own access there. */
export function usePublishDashboard(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { target: "opencti" | "opencti-custom-view" | "openaev"; integration_id: string; name?: string }) =>
      api.post<{ publication: DashboardPublication; notes: string[] }>(`/custom-dashboards/${id}/publish`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, id, "publications"] }),
  });
}

/** Share a dashboard on the XTM Hub (its OpenCTI export, as a new inactive resource of a dashboards library). */
export function useShareDashboardOnHub(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { integration_id: string; name?: string; short_description?: string }) =>
      api.post<{ publication: DashboardPublication; notes: string[] }>(`/custom-dashboards/${id}/share-on-hub`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...DASHBOARDS_KEY, id, "publications"] }),
  });
}
