// SPDX-License-Identifier: Apache-2.0

/** Invitation API hooks – admin link generation + public registration. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, setToken } from "@/lib/api";
import type {
  RegisterWithInvitationResponse,
  CreateInvitationRequest,
  CreateInvitationResponse,
  InvitationListItem,
  ValidateInvitationResponse,
} from "@/types";

// --- Admin ---
export function useInvitations(enabled = true) {
  return useQuery<InvitationListItem[]>({
    queryKey: ["invitations"],
    queryFn: () => api.get("/invitations"),
    enabled,
  });
}

export function useCreateInvitation() {
  const qc = useQueryClient();
  return useMutation<CreateInvitationResponse, Error, CreateInvitationRequest>({
    mutationFn: (data) => api.post("/invitations", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invitations"] }),
  });
}

export function useRevokeInvitation() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/invitations/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invitations"] }),
  });
}

// --- Public (register page) ---
export function useValidateInvitation(token: string | null) {
  return useQuery<ValidateInvitationResponse>({
    queryKey: ["invitation-validate", token],
    queryFn: () => api.get(`/invitations/validate/${token}`),
    enabled: !!token,
    retry: false,
  });
}

export function useRegisterWithInvitation() {
  return useMutation<
    RegisterWithInvitationResponse,
    Error,
    { token: string; password: string; full_name: string }
  >({
    mutationFn: (data) => api.post("/auth/register-with-invitation", data),
    onSuccess: (data) => setToken(data.access_token),
  });
}
