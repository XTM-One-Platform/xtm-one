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
 * The Deep Investigation pack builder: packs users build, their
 * versions, the built-in packs to duplicate, and a preview of what a pack
 * would do on a subject for the caller (no source queried).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unavailableIsOrdinary } from "@/lib/api";
import type {
  BuiltinPackItem,
  InvestigationPack,
  InvestigationPackFields,
  InvestigationPackVersionDetail,
  InvestigationPackVersionItem,
  PackBuilderCatalog,
  PackPreview,
} from "@/types";

const PACKS_KEY = ["investigation-packs"] as const;

/** Sharing fields, as every shareable entity takes them. */
interface PackSharing {
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean | null;
}

type SavePackInput = InvestigationPackFields & PackSharing & { commit_message?: string | null };

export function useInvestigationPacks(enabled = true) {
  return useQuery<InvestigationPack[]>({
    queryKey: PACKS_KEY,
    queryFn: () => api.get("/investigation-packs"),
    enabled,
    // 403 while DEEP_INVESTIGATION is off: an empty list, not a crash.
    ...unavailableIsOrdinary,
  });
}

export function useInvestigationPack(id: string) {
  return useQuery<InvestigationPack>({
    queryKey: [...PACKS_KEY, id],
    queryFn: () => api.get(`/investigation-packs/${id}`),
    enabled: !!id,
    // A deleted or unknown pack (404), or the feature off on this platform
    // (403), is shown as NotFoundState, not a crash.
    ...unavailableIsOrdinary,
  });
}

/** What the builder offers: kinds, reference sources, sets, icons, settings. */
export function usePackBuilderCatalog() {
  return useQuery<PackBuilderCatalog>({
    queryKey: [...PACKS_KEY, "catalog"],
    queryFn: () => api.get("/investigation-packs/catalog"),
    staleTime: Infinity,
    // 403 while DEEP_INVESTIGATION is off: the page says so.
    ...unavailableIsOrdinary,
  });
}

export function useBuiltinPacks() {
  return useQuery<BuiltinPackItem[]>({
    queryKey: [...PACKS_KEY, "builtin"],
    queryFn: () => api.get("/investigation-packs/builtin"),
    staleTime: Infinity,
  });
}

function invalidate(qc: ReturnType<typeof useQueryClient>, id?: string) {
  qc.invalidateQueries({ queryKey: PACKS_KEY });
  // The picker offers built packs: a saved change is what it offers next.
  qc.invalidateQueries({ queryKey: ["investigations", "subject-plan"] });
  if (id) qc.invalidateQueries({ queryKey: ["investigation-pack-versions", id] });
}

export function useCreateInvestigationPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: SavePackInput) => api.post<InvestigationPack>("/investigation-packs", data),
    onSuccess: () => invalidate(qc),
  });
}

export function useUpdateInvestigationPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: SavePackInput & { id: string }) =>
      api.put<InvestigationPack>(`/investigation-packs/${id}`, data),
    onSuccess: (_d, { id }) => invalidate(qc, id),
  });
}

export function useDeleteInvestigationPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/investigation-packs/${id}`),
    onSuccess: () => invalidate(qc),
  });
}

export function useDuplicateBuiltinPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => api.post<InvestigationPack>(`/investigation-packs/from-builtin/${slug}`, {}),
    onSuccess: () => invalidate(qc),
  });
}

export function useInvestigationPackVersions(id: string) {
  return useQuery<InvestigationPackVersionItem[]>({
    queryKey: ["investigation-pack-versions", id],
    queryFn: () => api.get(`/investigation-packs/${id}/versions`),
    enabled: !!id,
  });
}

export function useInvestigationPackVersion(id: string, version: number | null) {
  return useQuery<InvestigationPackVersionDetail>({
    queryKey: ["investigation-pack-versions", id, version],
    queryFn: () => api.get(`/investigation-packs/${id}/versions/${version}`),
    enabled: !!id && version !== null,
  });
}

export function useRestoreInvestigationPackVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      api.post<InvestigationPack>(`/investigation-packs/${id}/versions/${version}/restore`, {}),
    onSuccess: (_d, { id }) => invalidate(qc, id),
  });
}

/** What the pack being edited (``pack``) or a saved one (``pack_id``) would
 *  do on ``subject`` for the caller. A mutation: the draft travels in the
 *  body, and nothing is cached across edits. */
export function usePreviewInvestigationPack() {
  return useMutation({
    mutationFn: (body: {
      subject: string;
      pack?: InvestigationPackFields;
      pack_id?: string;
      options?: Record<string, string>;
    }) => api.post<PackPreview>("/investigation-packs/preview", body),
  });
}

/** One round with the builder's assistant: a reply and a pack the server
 *  already validated. Nothing is stored. */
export function useAssistInvestigationPack() {
  return useMutation({
    mutationFn: (body: {
      instruction: string;
      current: InvestigationPackFields | null;
      history: { role: "user" | "assistant"; content: string }[];
    }) => api.post<{ reply: string; pack: InvestigationPackFields }>("/investigation-packs/assist", body),
  });
}
