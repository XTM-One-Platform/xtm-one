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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { saveBlobAs } from "@/lib/utils";
import type {
  BrandAssetSource,
  BrandAssetSourceConfig,
  CreateTemplateContainerRequest,
  EntityListOptions,
  GenerateTemplateRequest,
  GenerateTemplateResponse,
  TemplateAsset,
  TemplateContainer,
  TemplateContainerDetail,
  TemplateContainerFilters,
  UpdateTemplateContainerRequest,
} from "@/types";

/** Build the structured-filter query string for the list endpoint.
 *  Mirrors the backend's `apply_structured_filters` contract: `name` is a
 *  substring, `kind` / `aspect_ratio` are exact, `tag` is repeatable, and
 *  `theme` is a colour scheme (`dark` / `light`, matched against a presentation
 *  template's `supported_schemes`) - any other value is matched exactly
 *  against the legacy free-text label. */
function buildFilterQuery(filters?: TemplateContainerFilters): string {
  if (!filters) return "";
  const params = new URLSearchParams();
  if (filters.name) params.set("name", filters.name);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.theme) params.set("theme", filters.theme);
  if (filters.aspect_ratio) params.set("aspect_ratio", filters.aspect_ratio);
  if (filters.match_all_tags) params.set("match_all_tags", "true");
  for (const tag of filters.tag ?? []) params.append("tag", tag);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** Cache key prefix shared by the list, the detail and the asset queries.
 *
 *  Every mutation invalidates this PREFIX rather than a container-scoped key.
 *  TanStack matches keys by prefix, so invalidating
 *  `["template-containers", <id>]` would NOT match the list query (keyed
 *  `["template-containers", <filters>]`) — and the list carries `asset_count`,
 *  so an asset upload would leave a stale count behind. */
const TEMPLATES_KEY = ["template-containers"] as const;

/** The templates list; `{ scope: "all" }` is every template for an admin (a deck's Design panel). */
export function useTemplateContainers(filters?: TemplateContainerFilters, options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<TemplateContainer[]>({
    // Filters are part of the key so each filter combination caches separately.
    queryKey: entityListKey([...TEMPLATES_KEY, filters ?? null], scope),
    queryFn: () => api.get(entityListPath(`/template-containers${buildFilterQuery(filters)}`, scope)),
  });
}

export function useTemplateContainer(id: string | undefined) {
  return useQuery<TemplateContainerDetail>({
    queryKey: [...TEMPLATES_KEY, id],
    queryFn: () => api.get(`/template-containers/${id}`),
    enabled: !!id,
  });
}

export function useCreateTemplateContainer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateTemplateContainerRequest) =>
      api.post<TemplateContainerDetail>("/template-containers", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

export function useUpdateTemplateContainer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & UpdateTemplateContainerRequest) =>
      api.put<TemplateContainerDetail>(`/template-containers/${id}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/** "Generate with AI": draft a complete template from a brief.
 *
 *  Nothing is persisted and no cache is touched - the response is a candidate
 *  the form applies, so there is deliberately no `onSuccess` invalidation. */
export function useGenerateTemplate() {
  return useMutation<GenerateTemplateResponse, Error, GenerateTemplateRequest>({
    mutationFn: (body) => api.post("/ai/generate-template", body),
  });
}

export function useDeleteTemplateContainer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/template-containers/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/**
 * Private, editable copy of a container the caller can see — metadata AND
 * assets. This is the only way to "edit" a built-in: the seed
 * full-syncs every field on boot, so an in-place edit would be reverted by the
 * next deploy.
 */
export function useDuplicateTemplateContainer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<TemplateContainerDetail>(`/template-containers/${id}/duplicate`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/** The long edges the thumbnail route serves (`THUMBNAIL_SIZES` in `services/template_thumbnails.py`).
 * @public
 */
export const TEMPLATE_THUMBNAIL_SIZES = [64, 128, 256] as const;
type TemplateThumbnailSize = (typeof TEMPLATE_THUMBNAIL_SIZES)[number];

/**
 * A bounded thumbnail of an image asset: what a tile draws instead of
 * the full file - a raster resized server-side, an SVG as stored. Root-relative
 * like every API path; `useAuthenticatedSrc` prefixes it at the sink.
 *
 * A replace keeps the asset id, and `useAuthenticatedSrc` refetches only when
 * the URL changes, so the URL carries the asset's `version`: it moves
 * with the bytes, whoever replaced them.
 */
export function templateAssetThumbnailUrl(
  containerId: string,
  asset: Pick<TemplateAsset, "id" | "version">,
  size: TemplateThumbnailSize = 128,
): string {
  return `/api/v1/template-containers/${containerId}/assets/${asset.id}/thumbnail?size=${size}&v=${encodeURIComponent(asset.version)}`;
}

/**
 * `throwOnError: false` for a caller that renders its own error state - the
 * deck editor's picture picker, where the app-wide default would hand
 * a 404 (a shared deck on a template its reader cannot see) to the route's
 * ErrorBoundary and drop the unsaved draft with the viewer.
 */
export function useTemplateAssets(containerId: string | undefined, options: { throwOnError?: boolean } = {}) {
  return useQuery<TemplateAsset[]>({
    queryKey: [...TEMPLATES_KEY, containerId, "assets"],
    queryFn: () => api.get(`/template-containers/${containerId}/assets`),
    enabled: !!containerId,
    ...(options.throwOnError === false ? { throwOnError: false } : {}),
  });
}

/** Add one asset. The container's other metadata is untouched — assets are
 *  managed independently, so this never requires re-sending the container. */
/** Query string for the asset upload / replace routes: the role and the
 *  caption travel as query parameters next to the multipart body. */
function assetMetaQuery(assetRole?: string, description?: string): string {
  const params = new URLSearchParams();
  if (assetRole) params.set("asset_role", assetRole);
  if (description) params.set("description", description);
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export function useAddTemplateAsset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      containerId,
      file,
      assetRole,
      description,
    }: {
      containerId: string;
      file: File;
      assetRole?: string;
      description?: string;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.postForm<TemplateAsset>(
        `/template-containers/${containerId}/assets${assetMetaQuery(assetRole, description)}`,
        formData,
      );
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/** Replace an asset's binary in place. The asset id is preserved, so stored
 *  layout references to it stay valid. */
export function useReplaceTemplateAsset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      containerId,
      assetId,
      file,
      assetRole,
      description,
    }: {
      containerId: string;
      assetId: string;
      file: File;
      assetRole?: string;
      description?: string;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.putForm<TemplateAsset>(
        `/template-containers/${containerId}/assets/${assetId}${assetMetaQuery(assetRole, description)}`,
        formData,
      );
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/** Relabel and/or re-describe an asset without re-uploading its binary.
 *
 *  Patch semantics: a field left `undefined` is NOT sent and stays as it is on
 *  the server; `null` clears it. Changing the role must never wipe the caption
 *  the seed or an admin wrote — the backend tells the two apart by which keys
 *  the body carries, so this hook only serializes the ones that were given. */
export function useUpdateTemplateAssetRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      containerId,
      assetId,
      assetRole,
      description,
    }: {
      containerId: string;
      assetId: string;
      assetRole?: string | null;
      description?: string | null;
    }) => {
      const body: { asset_role?: string | null; description?: string | null } = {};
      if (assetRole !== undefined) body.asset_role = assetRole;
      if (description !== undefined) body.description = description;
      return api.patch<TemplateAsset>(
        `/template-containers/${containerId}/assets/${assetId}`,
        body,
      );
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

export function useDeleteTemplateAsset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ containerId, assetId }: { containerId: string; assetId: string }) =>
      api.delete(`/template-containers/${containerId}/assets/${assetId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

/** Download an asset's binary. Returns the Blob plus the server-provided
 *  filename (from `Content-Disposition`), which is the authoritative name —
 *  the backend builds it with a CRLF-injection-safe helper, so prefer it over
 *  the `filename` carried in the asset row. */
export function useDownloadTemplateAsset() {
  return useMutation({
    mutationFn: ({ containerId, assetId }: { containerId: string; assetId: string }) =>
      api.download(`/template-containers/${containerId}/assets/${assetId}/download`),
  });
}

/** Export a container as a package ZIP (assets included) or a metadata-only
 *  JSON bundle.
 *
 *  `zip` is the default and the complete form: a template whose logo does not
 *  travel is not a template. `json` is offered for a palette-only template, and
 *  drops any binaries. */
export function useExportTemplateContainer() {
  return useMutation({
    mutationFn: async ({
      containerId,
      name,
      format = "zip",
    }: {
      containerId: string;
      name: string;
      format?: "zip" | "json";
    }) => {
      const { blob, filename } = await api.download(
        `/template-containers/${containerId}/export?format=${format}`,
      );
      const fallback = `template-${name
        .toLowerCase()
        .replace(/[^a-z0-9-_]+/g, "-")
        .slice(0, 60)}.${format}`;
      saveBlobAs(blob, filename ?? fallback);
    },
  });
}

/** Import a container from an export package — a ZIP or a JSON bundle.
 *
 *  The imported container is always owned by the caller and never
 *  company-managed: publishing a template org-wide stays a deliberate human
 *  action on the detail page. */
export function useImportTemplateContainer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.postForm<TemplateContainerDetail>(
        "/template-containers/import",
        formData,
      );
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: TEMPLATES_KEY }),
  });
}

const BRAND_ASSET_SOURCE_KEY = ["brand-asset-source"] as const;

/** The brand asset source (Settings > Platform, admin): a GitHub folder of brand packs. */
export function useBrandAssetSource() {
  return useQuery<BrandAssetSource>({
    queryKey: BRAND_ASSET_SOURCE_KEY,
    queryFn: () => api.get("/settings/brand-asset-source"),
  });
}

/** Set or clear the source; a set source is downloaded and seeded at once (`sync`). */
export function useSaveBrandAssetSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: BrandAssetSourceConfig) => api.put<BrandAssetSource>("/settings/brand-asset-source", body),
    onSuccess: (data) => {
      qc.setQueryData(BRAND_ASSET_SOURCE_KEY, data);
      qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
    // The route commits before it downloads: a timeout on the way back may
    // follow a save that went through, so read the source again.
    onError: () => {
      qc.invalidateQueries({ queryKey: BRAND_ASSET_SOURCE_KEY });
      qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
  });
}

/** Download the source again and seed its packs. */
export function useSyncBrandAssetSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<BrandAssetSource>("/settings/brand-asset-source/sync"),
    onSuccess: (data) => {
      qc.setQueryData(BRAND_ASSET_SOURCE_KEY, data);
      qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
    // A timeout on the way back may follow a seed that ran.
    onError: () => {
      qc.invalidateQueries({ queryKey: TEMPLATES_KEY });
    },
  });
}
