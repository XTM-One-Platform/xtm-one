// SPDX-License-Identifier: Apache-2.0

/** Supabase database access hooks (`/integrations/:id/supabase/...`). */

import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SupabaseProbeRequest, SupabaseProbeResponse } from "@/types";

/** Probe what a database role can be chosen from: nothing is stored. */
export function useSupabaseProbe(integrationId: string) {
  return useMutation<SupabaseProbeResponse, Error, SupabaseProbeRequest>({
    mutationFn: (body) => api.post(`/integrations/${integrationId}/supabase/probe`, body),
  });
}
