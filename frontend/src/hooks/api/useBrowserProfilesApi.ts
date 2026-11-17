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
 * Browser profile API hooks - `/api/v1/browser-profiles`.
 *
 * A profile is a saved sign-in every browser of the platform opens: an agent's
 * browser tool, the General Assistant's, a Video Studio recording. The list
 * polls while a sign-in runs or waits for a person's MFA code, and stops once
 * none does.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { api } from "@/lib/api";
import type { BrowserProfile, BrowserProfileWrite, BrowserSignInState } from "@/types";

const BROWSER_PROFILES_KEY = ["browser-profiles"] as const;
const LIVE_POLL_MS = 3_000;

export function useBrowserProfiles(options: { enabled?: boolean } = {}) {
  return useQuery<BrowserProfile[]>({
    queryKey: BROWSER_PROFILES_KEY,
    queryFn: async () => (await api.get<{ items: BrowserProfile[] }>("/browser-profiles")).items,
    enabled: options.enabled ?? true,
    refetchInterval: (query) =>
      query.state.data?.some((p) => p.session_status === "signing_in" || p.session_status === "awaiting_mfa")
        ? LIVE_POLL_MS
        : false,
  });
}

function useInvalidateProfiles() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: BROWSER_PROFILES_KEY });
}

export function useSaveBrowserProfile() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: ({ id, body }: { id?: string; body: BrowserProfileWrite }) =>
      id ? api.put<BrowserProfile>(`/browser-profiles/${id}`, body) : api.post<BrowserProfile>("/browser-profiles", body),
    onSuccess: invalidate,
  });
}

export function useDeleteBrowserProfile() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/browser-profiles/${id}`),
    onSuccess: invalidate,
  });
}

export function useSignInBrowserProfile() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: (id: string) => api.post<BrowserSignInState>(`/browser-profiles/${id}/sign-in`),
    onSettled: invalidate,
  });
}

/** Dry-run the whole sign-in: saves nothing, returns the step trace so the operator sees where it stalls. */
export function useTestBrowserProfileSignIn() {
  return useMutation({
    mutationFn: (id: string) => api.post<BrowserSignInState>(`/browser-profiles/${id}/test-sign-in`),
  });
}

export function useSignOutBrowserProfile() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: (id: string) => api.post<BrowserProfile>(`/browser-profiles/${id}/sign-out`),
    onSuccess: invalidate,
  });
}

/** Probe the saved session: the start URL opened in it, checked to render the application. */
export function useCheckBrowserProfileSession() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: (id: string) => api.post<BrowserProfile>(`/browser-profiles/${id}/check`),
    onSettled: invalidate,
  });
}

export function useSubmitMfaCode() {
  const invalidate = useInvalidateProfiles();
  return useMutation({
    mutationFn: ({ challengeId, code }: { challengeId: string; code: string }) =>
      api.post<BrowserSignInState>(`/browser-profiles/mfa/${encodeURIComponent(challengeId)}`, { code }),
    onSettled: invalidate,
  });
}

/** The screenshot of a profile's open MFA prompt as an image `src`, or `null` while it loads or without one. */
export function useMfaScreenshot(profile: BrowserProfile): string | null {
  const fileId = profile.mfa_challenge?.screenshot_file_id;
  return useAuthenticatedSrc(
    fileId ? `/api/v1/browser-profiles/${profile.id}/mfa-screenshot?file=${encodeURIComponent(fileId)}` : null,
  );
}
