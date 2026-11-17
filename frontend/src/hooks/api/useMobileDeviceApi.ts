// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { LinkedPhone, NotificationKind } from "@/types";

const PHONES_KEY = ["mobile-devices"] as const;

/** The person's linked phones; `endpointHash` marks the one this page runs on. */
export function useLinkedPhones(endpointHash?: string | null) {
  return useQuery<LinkedPhone[]>({
    queryKey: [...PHONES_KEY, endpointHash ?? null],
    queryFn: () => api.get(`/mobile-devices${endpointHash ? `?endpoint_hash=${endpointHash}` : ""}`),
  });
}

export interface PhoneUpdate {
  name?: string;
  push_enabled?: boolean;
  muted_kinds?: NotificationKind[];
  quiet_hours?: { start: string; end: string; timezone: string };
  clear_quiet_hours?: boolean;
}

export function useUpdatePhone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: PhoneUpdate & { id: string }) => api.patch<LinkedPhone>(`/mobile-devices/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: PHONES_KEY }),
  });
}

export function useUnlinkPhone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/mobile-devices/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: PHONES_KEY }),
  });
}

export function useTestPhone() {
  return useMutation({
    mutationFn: (id: string) => api.post<{ sent: boolean }>(`/mobile-devices/${id}/test`),
  });
}

/** Re-read the phone list after this page linked its phone. */
export function useRefreshPhones() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: PHONES_KEY });
}
