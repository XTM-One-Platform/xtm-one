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
 * The browser half of the XTM One Desktop sign-in handoff - what the
 * login page calls when an SSO callback lands with `desktop_login` in its
 * fragment. The desktop half (start, exchange, cancel) is the native shell's:
 * it holds the runner credential, the page never does.
 *
 * The session being handed over lives in React state only (the tab stays
 * signed out), so the two writes take the bearer explicitly (`api.postAs`)
 * instead of reading the stored token like every other hook.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import type { DesktopLoginHandoff } from "@/types";

/** Which machine is asking - the confirmation page's one read. A 404 means the request expired or was declined. */
export function useDesktopLoginHandoff(loginId: string | null) {
  return useQuery<DesktopLoginHandoff>({
    queryKey: ["desktop-login", loginId],
    queryFn: () => api.get(`/desktop-login/${encodeURIComponent(loginId as string)}`),
    enabled: !!loginId,
    ...notFoundIsOrdinary,
  });
}

/** "Yes, sign me in there": hand the session to the app. */
export function useCompleteDesktopLogin() {
  return useMutation({
    mutationFn: ({ loginId, token }: { loginId: string; token: string }) =>
      api.postAs<{ status: string; runner_name: string }>(
        `/desktop-login/${encodeURIComponent(loginId)}/complete`,
        token,
      ),
  });
}

/** "Not my machine": drop the request (audited) so nobody else can complete it in its window. */
export function useDeclineDesktopLogin() {
  return useMutation({
    mutationFn: ({ loginId, token }: { loginId: string; token: string }) =>
      api.postAs<void>(`/desktop-login/${encodeURIComponent(loginId)}/decline`, token),
  });
}
