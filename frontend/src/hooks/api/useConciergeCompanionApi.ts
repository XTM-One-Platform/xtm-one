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
 * The Concierge companion (#8261): the pet library, the signed-in user's
 * companion and what it shows. Built in since #101 (S1), no flag;
 * callers pass `enabled` for their own reasons.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { ConciergeActivity, ConciergeCompanion, ConciergePet } from "@/types";

const PETS_KEY = ["concierge", "pets"] as const;
const COMPANION_KEY = ["concierge", "companion"] as const;
/** How often the pet asks what to show: every read is a few counts, no model call. */
const ACTIVITY_INTERVAL_MS = 30_000;

export function usePets(enabled = true) {
  return useQuery<ConciergePet[]>({ queryKey: PETS_KEY, queryFn: () => api.get("/concierge/pets"), enabled });
}

export function useUploadPet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData();
      for (const file of files) form.append("files", file, file.name);
      return api.postForm<ConciergePet>("/concierge/pets", form);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: PETS_KEY }),
  });
}

export function useDeletePet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => api.delete(`/concierge/pets/${encodeURIComponent(slug)}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: PETS_KEY });
      void qc.invalidateQueries({ queryKey: COMPANION_KEY });
    },
  });
}

/** An object URL of a pet's spritesheet, fetched with the session, revoked when it changes. */
export function usePetSpritesheet(slug: string | undefined, sha256: string | undefined): string | null {
  const { data } = useQuery<Blob>({
    queryKey: ["concierge", "pet-spritesheet", slug, sha256],
    queryFn: async () => (await api.download(`/concierge/pets/${encodeURIComponent(slug as string)}/spritesheet`)).blob,
    enabled: Boolean(slug),
    staleTime: Infinity,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!data) {
      // Another pet is loading: show none rather than a revoked URL.
      setUrl(null);
      return undefined;
    }
    const next = URL.createObjectURL(data);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [data]);
  return url;
}

export function useCompanion(enabled = true) {
  return useQuery<ConciergeCompanion>({
    queryKey: COMPANION_KEY,
    queryFn: () => api.get("/concierge/me/companion"),
    enabled,
  });
}

export function useSaveCompanion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { enabled: boolean; pet: string | null }) =>
      api.put<ConciergeCompanion>("/concierge/me/companion", body),
    onSuccess: (data) => qc.setQueryData(COMPANION_KEY, data),
  });
}

export function useCompanionActivity(enabled: boolean) {
  return useQuery<ConciergeActivity>({
    queryKey: ["concierge", "activity"],
    queryFn: () => api.get("/concierge/me/activity"),
    enabled,
    refetchInterval: enabled ? ACTIVITY_INTERVAL_MS : false,
    refetchIntervalInBackground: false,
  });
}
