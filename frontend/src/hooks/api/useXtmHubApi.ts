// SPDX-License-Identifier: Apache-2.0

/** This platform's registration with the XTM Hub (Settings > Platform, home dashboard pill). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { XtmHubRegistration, XtmHubRegistrationStatusInfo } from "@/types";

const REGISTRATION_KEY = ["xtm-hub", "registration"] as const;
const STATUS_KEY = ["xtm-hub", "registration", "status"] as const;

/** The registration and what the Hub's register page needs (admin). */
export function useXtmHubRegistration(enabled = true) {
  return useQuery<XtmHubRegistration>({
    queryKey: REGISTRATION_KEY,
    queryFn: () => api.get("/xtm-hub/registration"),
    enabled,
  });
}

/** The coarse registration status the home dashboard pill shows (any user). */
export function useXtmHubRegistrationStatus() {
  return useQuery<XtmHubRegistrationStatusInfo>({
    queryKey: STATUS_KEY,
    queryFn: () => api.get("/xtm-hub/registration/status"),
    staleTime: 60_000,
  });
}

function useRegistrationMutation<TVariables>(mutationFn: (variables: TVariables) => Promise<XtmHubRegistration>) {
  const qc = useQueryClient();
  return useMutation<XtmHubRegistration, Error, TVariables>({
    mutationFn,
    onSuccess: (registration) => {
      qc.setQueryData(REGISTRATION_KEY, registration);
      qc.invalidateQueries({ queryKey: STATUS_KEY });
    },
  });
}

/** Store the platform token the Hub's page posted back, once the Hub confirms it. */
export function useRegisterXtmHub() {
  return useRegistrationMutation((body: { hub_url: string; token: string }) =>
    api.post("/xtm-hub/registration", body),
  );
}

/** Ask the Hub about the registration now. */
export function useCheckXtmHubRegistration() {
  return useRegistrationMutation((_: void) => api.post("/xtm-hub/registration/check", {}));
}

/** Forget the registration locally. */
export function useForgetXtmHubRegistration() {
  return useRegistrationMutation((_: void) => api.delete("/xtm-hub/registration"));
}
